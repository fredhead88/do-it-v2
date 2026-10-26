#!/usr/bin/env python3
"""Resource lifecycle checks against private ledgers and real filesystem/git state."""
import atexit
import ast
from datetime import datetime, timedelta, timezone
import inspect
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

# Do this before importing fold, including under the unchanged ./doit test runner.
_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix="doit-resources-")
atexit.register(_IMPORT_ROOT.cleanup)
os.environ["DOIT_ROOT"] = _IMPORT_ROOT.name
os.environ['HOME'] = _IMPORT_ROOT.name
if Path(os.environ["DOIT_ROOT"]).resolve() == (Path.home() / ".do-it").resolve():
    raise RuntimeError("resource tests require a throwaway DOIT_ROOT")
os.environ.pop("DOIT_PROJECT", None)
os.environ["DOIT_LEDGER_FILE"] = "L-tick-local.jsonl"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fold
import resources
import fold_emits
import disk_reaper
import reap_tmp
disk_reaper.LOOK_TOML_PATH = Path(_IMPORT_ROOT.name) / 'look.toml'


def event(event_type, subject="L-spec-0001", **fields):
    return dict(type=event_type, subject=subject, actor=fields.pop("actor", "operator"),
                ts="2026-10-23T00:00:00+00:00", **fields)


def created(path, kind="tmpdir", subject="L-spec-0001"):
    return event("resource-created", subject, kind=kind, ref=str(path), lane="build")


def finished(subject="L-spec-0001"):
    return [event("spec-written", subject), event("spec-killed", subject)]


def evidence1654(ac, body):
    body = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url='cli://resources/L1654-' + ac, status=0,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))


class ResourcesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="doit-resource-case-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        p = patch.object(disk_reaper, 'LOOK_TOML_PATH', self.root / 'look.toml')
        p.start()
        self.addCleanup(p.stop)
        self.ledger = self.root / "events" / "L-tick-local.jsonl"
        for target, value in (("ROOT", self.root), ("EVENTS", self.root / "events"),
                              ("BOARD", self.root / "board.md")):
            p = patch.object(fold, target, value)
            p.start()
            self.addCleanup(p.stop)
        # A controlled proc fixture resolves real self/child process entries.
        # Unreadable process entries are tested separately and must refuse.
        self.proc = self.root / "proc"
        self.proc.mkdir()
        (self.proc / str(os.getpid())).symlink_to(Path("/proc") / str(os.getpid()))
        (self.proc / "net").mkdir()
        (self.proc / "net" / "unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        p = patch.object(resources, "PROC_ROOT", self.proc)
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict(os.environ, DOIT_ROOT=str(self.root),
                       DOIT_LEDGER_FILE="L-tick-local.jsonl")
        p.start()
        self.addCleanup(p.stop)

    def fresh(self):
        result = subprocess.run(
            ["/usr/bin/python3", "-c",
             "import json,fold; print(json.dumps(fold.read_events(use_cache=False)))"],
            cwd=Path(__file__).resolve().parent, env=os.environ.copy(),
            capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def bytes(self):
        return self.ledger.read_bytes() if self.ledger.exists() else b""

    def directory(self, name):
        path = self.root / name
        path.mkdir()
        (path / "payload").write_text("fixture")
        return path

    def git(self, repo, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        return subprocess.run(["git", "-C", str(repo), *args], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def worktree(self):
        repo = self.root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        (repo / "seed").write_text("seed")
        self.git(repo, "add", "seed")
        self.git(repo, "commit", "-qm", "seed")
        worktrees = self.root / "worktrees"
        worktrees.mkdir()
        wt = worktrees / "wt"
        self.git(repo, "worktree", "add", "-q", "-b", "l-spec-0001", str(wt))
        sha = self.git(wt, "rev-parse", "HEAD")
        evs = finished() + [event("build-done", branch="l-spec-0001", ready_sha=sha),
                            created(wt, "worktree")]
        return repo, wt, evs

    def test_ac1_ledger_path_is_single_source(self):
        for root in (self.root / "events", self.root / "other-events"):
            with patch.object(fold, "EVENTS", root):
                for filename in (None, "L-build-local.jsonl", str(self.root / "absolute.jsonl")):
                    with self.subTest(root=root, filename=filename), patch.dict(os.environ):
                        if filename is None:
                            os.environ.pop("DOIT_LEDGER_FILE", None)
                        else:
                            os.environ["DOIT_LEDGER_FILE"] = filename
                        self.assertEqual(fold.ledger_path(), root / (filename or "L-operator-local.jsonl"))
                        self.assertEqual(fold.ledger_path(inbox=True), root / "L-inbox-local.jsonl")
        tree = ast.parse(Path(fold.__file__).read_text())
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "ledger_path" for node in ast.walk(functions["append"])))
        occurrences = [node for node in ast.walk(tree) if isinstance(node, ast.Constant)
                       and node.value == "L-inbox-local.jsonl"]
        self.assertEqual(len(occurrences), 1)
        self.assertIn(occurrences[0], list(ast.walk(functions["ledger_path"])))

    def test_ac2_append_bytes_unchanged(self):
        # Run the same append body with the original literal path expression
        # as an independent routing/bytes oracle, not a second writer model.
        original_expression = ('path = EVENTS / ("L-inbox-local.jsonl" if inbox else '
                               'os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl"))')
        source = inspect.getsource(fold.append)
        self.assertEqual(source.count("path = ledger_path(inbox=inbox)"), 1)
        legacy_source = source.replace("path = ledger_path(inbox=inbox)", original_expression)
        for filename, inbox in ((None, False), ("L-build-local.jsonl", False),
                                (str(self.root / "events/L-builder-local.jsonl"), False),
                                ("L-build-local.jsonl", True)):
            with self.subTest(filename=filename, inbox=inbox), patch.dict(os.environ):
                if filename is None:
                    os.environ.pop("DOIT_LEDGER_FILE", None)
                else:
                    os.environ["DOIT_LEDGER_FILE"] = filename
                argv = ["resource-created", "L-spec-0001", 'kind="tmpdir"', 'ref="fixture"',
                        'lane="build"', 'project="resource-test"'] + (["--inbox"] if inbox else [])
                expected_path = fold.EVENTS / ("L-inbox-local.jsonl" if inbox else
                                               os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl"))
                expected = dict(v=1, ts="2026-10-25T12:00:00+00:00", type="resource-created",
                                subject="L-spec-0001", kind="tmpdir", ref="fixture", lane="build",
                                project="resource-test")
                with patch.object(fold, "NOW", datetime.fromisoformat(expected["ts"])):
                    namespace = dict(vars(fold))
                    exec(compile(legacy_source, "<original-path-append>", "exec"), namespace)
                    original = namespace["append"](argv, strict=True)
                    original_bytes = expected_path.read_bytes()
                    self.assertEqual(set(fold.EVENTS.iterdir()), {expected_path})
                    expected_path.unlink()
                    written = fold.append(argv, strict=True)
                self.assertEqual(written, original)
                self.assertEqual(expected_path.read_bytes(), original_bytes)
                self.assertEqual(written, expected)
                self.assertEqual(expected_path.read_bytes(), (json.dumps(expected, sort_keys=True) + "\n").encode())
                self.assertEqual(set(fold.EVENTS.iterdir()), {expected_path})
                expected_path.unlink()

    def test_ac3_record_reads_no_full_ledger(self):
        fold.EVENTS.mkdir()
        wrong = event("resource-created", kind="tmpdir", ref="fixture", lane="build")
        (fold.EVENTS / "L-other-local.jsonl").write_text(json.dumps(wrong) + "\n")
        with patch.object(fold, "PROJECT", "resource-test"), \
                patch.object(fold, "read_events", side_effect=AssertionError("full ledger")), \
                patch.object(fold, "_read_events_uncached", side_effect=AssertionError("uncached ledger")):
            row = resources.record("tmpdir", "fixture", lane="build", subject="L-spec-0001")
        self.assertEqual(row["_src"], "L-tick-local.jsonl:1")
        self.assertEqual(row["project"], "resource-test")
        tree = ast.parse(inspect.getsource(resources.record))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Name, ast.Attribute)):
                self.assertNotIn(node.id if isinstance(node, ast.Name) else node.attr,
                                 ("read_events", "_read_events_uncached"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "next":
                self.assertGreaterEqual(len(node.args), 2)

    def test_ac4_record_matches_fold_shape(self):
        for filename in (None, "L-build-local.jsonl", str(self.root / "events/L-builder-local.jsonl"),
                         "L-hidden-local.jsonl"):
            with self.subTest(filename=filename), patch.dict(os.environ):
                if filename is None:
                    os.environ.pop("DOIT_LEDGER_FILE", None)
                else:
                    os.environ["DOIT_LEDGER_FILE"] = filename
                hidden = filename == "L-hidden-local.jsonl"
                with patch.object(fold, "PROJECT", "hidden-project" if hidden else None):
                    row = resources.record("tmpdir", "fixture", lane="build", subject="L-spec-0001")
                if hidden:
                    with patch.dict(os.environ, DOIT_PROJECT="visible-project"):
                        self.assertNotIn(row, self.fresh())
                    expected = [e for e in fold._read_events_uncached() if e["_src"] == row["_src"]][0]
                else:
                    expected = [e for e in self.fresh() if e["_src"] == row["_src"]][0]
                self.assertEqual(row, expected)
                self.assertEqual(row["actor"], fold.actor_of(Path(filename or "L-operator-local.jsonl").name))
        with patch.object(fold, "PROJECT", "resource-test"):
            first = resources.record("tmpdir", "duplicate", lane="build", subject="L-spec-0001")
            second = resources.record("tmpdir", "duplicate", lane="build", subject="L-spec-0001")
        self.assertEqual(first | {"_src": second["_src"]}, second)
        self.assertEqual(int(second["_src"].rsplit(":", 1)[1]), int(first["_src"].rsplit(":", 1)[1]) + 1)

    def test_ac5_line_number_matches_fold_split_rule(self):
        fold.EVENTS.mkdir()
        # Put a UTF-8 character across the first streaming boundary. Include
        # splitlines separators both inside (invalid) JSON and between rows.
        seed = b" " * 63 + "é\n".encode()
        foreign = json.dumps(event("fixture")) + "\r\n\n\r"
        for separator in ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            foreign += '{"raw":"x' + separator + 'y"}' + separator
        seed += ((foreign + "torn JSON\n") * 25).encode()
        self.ledger.write_bytes(seed)
        with patch.object(resources, "_READ_CHUNK", 64), patch.object(resources, "_TAIL_WINDOW", 512), \
                patch.object(fold, "PROJECT", "resource-test"):
            row = resources.record("tmpdir", "fixture", lane="build", subject="L-spec-0001")
        expected = [e for e in fold._read_events_uncached() if e.get("type") == "resource-created"][0]
        self.assertEqual(row, expected)
        window_lines = len(self.ledger.read_bytes()[-512:].decode().splitlines())
        self.assertGreater(int(row["_src"].rsplit(":", 1)[1]), window_lines)
        self.assertGreater(len(seed), 512 + 64)

    def test_ac6_record_memory_is_bounded(self):
        fold.EVENTS.mkdir()
        line = (json.dumps(event("fixture", padding="x" * 1000)) + "\n").encode()
        with self.ledger.open("wb") as stream:
            for _ in range((24 * 1024 * 1024) // len(line)):
                stream.write(line)
        original_append, original_open = fold.append, Path.open
        requests = []

        class TrackedFile:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def read(self, size=-1):
                requests.append(size)
                if not 0 <= size <= resources._READ_CHUNK:
                    raise AssertionError(f"unbounded read: {size}")
                return self.stream.read(size)

            def __getattr__(self, key):
                return getattr(self.stream, key)

        def append_then_measure(*args, **kwargs):
            written = original_append(*args, **kwargs)
            tracemalloc.start()
            return written

        def tracked_open(path, *args, **kwargs):
            return TrackedFile(original_open(path, *args, **kwargs))

        try:
            with patch.object(fold, "PROJECT", "resource-test"), \
                    patch.object(fold, "append", side_effect=append_then_measure), \
                    patch.object(Path, "open", tracked_open), \
                    patch.object(Path, "read_text", side_effect=AssertionError("whole-file read_text")):
                row = resources.record("tmpdir", "fixture", lane="build", subject="L-spec-0001")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(row["ref"], "fixture")
        self.assertTrue(requests)
        self.assertLess(peak, 12 * 1024 * 1024)
        print(f"readback memory: peak={peak} bytes; max read={max(requests)} bytes")

    def test_ac7_miss_raises_named_error(self):
        self.assertTrue(issubclass(resources.RecordReadbackError, RuntimeError))
        self.assertFalse(issubclass(resources.RecordReadbackError, StopIteration))
        original = fold.append
        for case in ("miss", "missing", "directory", "beyond-window", "unparseable"):
            filename = f"L-{case}-local.jsonl"
            path = fold.EVENTS / filename

            def changed_append(*args, **kwargs):
                written = original(*args, **kwargs)
                if case == "miss":
                    return written | {"ref": "not-written"}
                if case == "missing":
                    path.unlink()
                elif case == "directory":
                    path.unlink()
                    path.mkdir()
                elif case == "beyond-window":
                    with path.open("ab") as stream:
                        stream.write(b"{}\n" * 2000)
                elif case == "unparseable":
                    path.write_bytes(b"\xffnot JSON\n")
                return written

            with self.subTest(case=case), patch.dict(os.environ, DOIT_LEDGER_FILE=filename), \
                    patch.object(resources, "_TAIL_WINDOW", 128), \
                    patch.object(fold, "PROJECT", "resource-test"), \
                    patch.object(fold, "append", side_effect=changed_append):
                with self.assertRaises(resources.RecordReadbackError) as raised:
                    resources.record("tmpdir", "fixture", lane="build", subject="L-spec-miss")
                for word in (filename, "resource-created", "L-spec-miss"):
                    self.assertIn(word, str(raised.exception))

        def concurrent_append(*args, **kwargs):
            written = original(*args, **kwargs)
            with self.ledger.open("ab") as stream:
                stream.write(b'{}\n{"other":"writer"}\n')
            return written

        with patch.object(fold, "PROJECT", "resource-test"), \
                patch.object(fold, "append", side_effect=concurrent_append):
            row = resources.record("tmpdir", "fixture", lane="build", subject="L-spec-concurrent")
        self.assertEqual(row["_src"], "L-tick-local.jsonl:1")
        self.assertEqual(row["subject"], "L-spec-concurrent")

    def test_ac8_signatures_and_release_round_trip(self):
        self.assertEqual(str(inspect.signature(resources.record)),
                         "(kind: str, ref: str, *, lane: str, subject: str) -> dict")
        self.assertEqual(str(inspect.signature(resources.release)), "(row: dict, *, reason: str)")
        path = self.directory("round-trip")
        row = resources.record("tmpdir", str(path), lane="build", subject="live-operation")
        self.assertEqual(sum(p.exists() for p in [path]), 1)
        resources.release(row, reason="creator-finally")
        self.assertFalse(path.exists())
        self.assertEqual(resources.live(self.fresh()), [])

    def test_record_round_trip(self):
        os.environ["DOIT_LEDGER_FILE"] = "L-build-local.jsonl"
        result = resources.record("worktree", "/tmp/x/wt-1", lane="build", subject="L-spec-0001")
        path = self.root / "events" / "L-build-local.jsonl"
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertNotIn("actor", json.loads(lines[0]))
        rows = self.fresh()
        self.assertEqual(len(rows), 1)
        for key, value in dict(type="resource-created", kind="worktree", ref="/tmp/x/wt-1",
                               lane="build", subject="L-spec-0001", actor="build").items():
            self.assertEqual(rows[0][key], value)
            self.assertEqual(result[key], value)

    def test_record_refuses(self):
        resources.record("tmpdir", "seed", lane="build", subject="L-spec-0001")
        before = self.bytes()
        for change in (dict(kind="dir"), dict(ref=""), dict(lane=""), dict(subject="")):
            args = dict(kind="tmpdir", ref="x", lane="build", subject="L-spec-0001") | change
            with self.assertRaises(ValueError):
                resources.record(**args)
            self.assertEqual(before, self.bytes())
        for kind in ("worktree", "scratch-db", "venv", "upload-copy", "tmpdir"):
            resources.record(kind, kind, lane="build", subject="L-spec-0001")
        self.assertEqual(len(self.fresh()), 6)
        # JSON-looking field strings must remain strings through fold.append.
        row = resources.record("tmpdir", "null", lane="true", subject="--inbox")
        self.assertEqual((row["ref"], row["lane"], row["subject"]), ("null", "true", "--inbox"))

    def test_live_basic(self):
        evs = [created("/one"), created("/gone"), event("resource-reaped", ref="/gone", actor="tick"),
               created("/two")]
        self.assertEqual({r.ref for r in resources.live(evs)}, {"/one", "/two"})

    def test_dry_run(self):
        _, wt, evs = self.worktree()
        tmp = self.directory("scratch")
        before = self.bytes()
        rows = resources.reap(evs + [created(tmp)])
        self.assertEqual([r.action for r in rows], ["would-reap", "would-reap"], rows)
        self.assertTrue(wt.is_dir() and tmp.is_dir())
        self.assertEqual(before, self.bytes())

    def test_reap_safety_removes(self):
        _, wt, evs = self.worktree()
        paths = [self.directory(kind) for kind in ("venv", "tmpdir")]
        evs += [created(p, p.name) for p in paths]
        self.assertEqual(sum(p.exists() for p in [wt] + paths), 3)
        rows = resources.reap(evs, dry_run=False)
        self.assertEqual([r.action for r in rows], ["reaped"] * 3, rows)
        self.assertTrue(all(not p.exists() for p in [wt] + paths))
        self.assertEqual(len(self.fresh()), 3)
        print("removal preflight: recorded existing paths=3; confirmed gone=3")

    def assert_refused(self, path, evs, prefix=None, **kwargs):
        before = self.bytes()
        self.assertTrue(path.exists())
        rows = resources.reap(evs, dry_run=False, **kwargs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "refused")
        if prefix:
            self.assertTrue(rows[0].reason.startswith(prefix), rows[0].reason)
        self.assertTrue(path.exists())
        self.assertEqual(before, self.bytes())
        return rows[0]

    def test_reap_safety_refuses(self):
        path = self.directory("retained")
        self.assert_refused(path, [event("spec-written"), created(path)], "owner not terminal")
        self.assert_refused(path, [created(path)], "undetermined: owner")
        self.assert_refused(path, finished() + [created(path)], "undetermined:",
                            safety=lambda *_: (_ for _ in ()).throw(RuntimeError("probe failed")))
        self.assert_refused(path, finished() + [created(path)], "custom hold", safety=lambda *_: "custom hold")
        result = resources.reap(finished() + [created(path, "scratch-db")], dry_run=False)
        self.assertEqual(result[0].action, "reaped")
        self.assertFalse(path.exists())

    def test_worktree_adapter_refuses(self):
        repo, wt, evs = self.worktree()
        (wt / "dirty").write_text("untracked")
        self.assertIn("clean", self.assert_refused(wt, evs).reason)
        (wt / "dirty").unlink()
        (wt / "seed").write_text("unmerged")
        self.git(wt, "commit", "-qam", "unmerged")
        self.assertIn("holds work", self.assert_refused(wt, evs).reason)
        self.git(wt, "reset", "--hard", "main")
        live = event("build-started", spawn="L-builder-0001")
        self.assert_refused(wt, evs + [live], "builder spawn live")
        self.assert_refused(wt, evs + [event("rejected-criterion", criterion="AC1", actor="reviewer")],
                            "standing rejection")
        self.assertTrue(repo.is_dir())
        self.git(repo, "update-ref", "-d", "refs/heads/l-spec-0001")
        self.assert_refused(wt, evs, "undetermined:")

    def test_worktree_adapter_wrong_path(self):
        _, wt, evs = self.worktree()
        wrong = wt.parent / "wrong"
        wrong.mkdir()
        self.assert_refused(wrong, evs[:-1] + [created(wrong, "worktree")], "undetermined:")

    def test_worktree_adapter_reaps(self):
        _, wt, evs = self.worktree()
        self.assertEqual(sum(p.exists() for p in [wt]), 1)
        rows = resources.reap(evs, dry_run=False)
        self.assertEqual(rows[0].action, "reaped", rows)
        self.assertFalse(wt.exists())
        self.assertEqual(len(self.fresh()), 1)
        print("removal preflight: recorded worktrees=1; confirmed gone=1")

    def test_reap_safety_process(self):
        path = self.directory("held")
        evs = finished() + [created(path)]
        # Real live cwd and real live fd, with the child readiness synchronized.
        for script in ("import os,sys; os.chdir(sys.argv[1]); print('ready',flush=True); input()",
                       "import sys; f=open(sys.argv[1]+'/payload'); print('ready',flush=True); input()"):
            child = subprocess.Popen([sys.executable, "-c", script, str(path)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                entry = self.proc / str(child.pid)
                entry.symlink_to(Path("/proc") / str(child.pid))
                self.assert_refused(path, evs, "live process")
                entry.unlink()
            finally:
                child.communicate("\n", timeout=5)
                child.stdout.close()
        with patch.object(resources.reap_tmp, "_pid_refs", side_effect=PermissionError("unreadable pid")):
            self.assert_refused(path, evs, "undetermined:")

    def thresholds1654(self, **values):
        disk_reaper.LOOK_TOML_PATH.write_text('[thresholds]\n' +
            '\n'.join(key + '=' + json.dumps(value) for key, value in values.items()))

    def test_1654_recorded_process_classifier(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (child.terminate(), child.wait()))
        pid = self.proc / str(child.pid)
        pid.mkdir()
        (pid / 'status').write_text(f'Uid: {os.getuid()} {os.getuid()} {os.getuid()} {os.getuid()}\nCapPrm: 8\n')
        (pid / 'cmdline').write_bytes(b'/usr/lib/systemd/systemd\0--user\0')
        (self.proc / 'self').mkdir()
        (self.proc / 'self/status').write_text('CapEff: 0\n')
        real_stat, real_refs = os.lstat, reap_tmp._pid_refs
        def other_uid(path, *args, **kw):
            st = real_stat(path, *args, **kw)
            if Path(path) == pid:
                fields = list(st)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return st
        def refs(proc, number):
            if number == child.pid:
                raise PermissionError('child pid')
            return real_refs(proc, number)
        for skip in (True, False):
            path = self.directory('process-' + str(skip))
            self.assertEqual(sum(p.exists() for p in [path]), 1)
            with (patch.object(reap_tmp, '_pid_refs', side_effect=refs),
                  patch.object(os, 'lstat', side_effect=other_uid if skip else real_stat)):
                self.assertEqual(resources.reap(finished() + [created(path)], dry_run=False)[0].action, 'reaped')
            self.assertFalse(path.exists())
        evidence1654('AC2', dict(other_uid_reaped=1, exempt_same_uid_reaped=1, paths_remaining=0))

    def test_1654_single_fold_and_live_counters(self):
        events = finished() + [event('spec-written', 'L-spec-0002')]
        for i in range(30):
            events.append(created(self.directory(f'count-{i}'),
                'scratch-db' if i < 20 else 'tmpdir',
                'L-spec-0001' if i < 20 else 'L-spec-0002'))
        base = self.root
        for i in range(3):
            case_root = base / 'worktrees' / f'case-{i}'
            case_root.mkdir(parents=True)
            with patch.object(self, 'root', case_root):
                repo, _, evs = self.worktree()
                sid = f'L-spec-{10 + i:04}'
                self.git(repo, 'branch', '-m', 'l-spec-0001', sid.lower())
                evs = [dict(e, subject=sid, **({'branch': sid.lower()} if 'branch' in e else {})) for e in evs]
            events += evs
        for dry in (True, False):
            with (patch.object(fold, 'fold', wraps=fold.fold) as folded,
                  patch.object(fold, 'terminal_spawns', wraps=fold.terminal_spawns) as ended,
                  patch.object(resources, 'live', wraps=resources.live) as live):
                rows = reap_tmp._reap_recorded(events, dry_run=dry, proc_root=self.proc)
            self.assertEqual(sum(r.action == ('would-reap' if dry else 'reaped') for r in rows), 23)
            self.assertEqual(sum(r.action == 'refused' for r in rows), 10)
            self.assertLessEqual(folded.call_count, 1)
            self.assertLessEqual(ended.call_count, 1)
            self.assertEqual(live.call_count, 1)
            self.assertIsNone(resources._REAP_MEMO.get())
            evidence1654('AC3', dict(dry_run=dry, fold=folded.call_count,
                terminal_spawns=ended.call_count, live=live.call_count,
                removable=sum(r.action in ('reaped', 'would-reap') for r in rows),
                refused=sum(r.action == 'refused' for r in rows)))
        # The adapter remains usable without a memo, on fresh real git state.
        with patch.object(self, 'root', base / 'direct'):
            self.root.mkdir()
            _, wt, evs = self.worktree()
            self.assertIsNone(resources._default_safety(resources.live(evs)[0], evs))

    def test_1654_retirement_roundtrip_and_identity(self):
        now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        old = (now - timedelta(hours=7)).isoformat()
        subject = 'L-spec-0001-abcdef123456-42'
        ref = str(self.root / 'absent')
        resources._append('spec-written', subject, project='do-it-v2', ts=old)
        resources._append('resource-created', subject, kind='scratch-db', ref=ref,
                          lane='build', project='albert-scott', ts=old)
        events = self.fresh()
        before = self.bytes()
        with patch.dict(os.environ, DOIT_LEDGER_FILE='L-builder-local.jsonl'):
            self.assertEqual(resources.reap(events, dry_run=False, now=now)[0].reason, 'actor-not-admitted')
            self.assertEqual(resources.reap(events, now=now)[0].action, 'would-retire')
        self.assertEqual(resources.reap(events, now=now)[0].action, 'would-retire')
        self.assertEqual(self.bytes(), before)
        with (patch.object(fold, 'PROJECT', None), patch.object(fold, 'NOW', now - timedelta(hours=10))):
            self.assertEqual(resources.reap(events, dry_run=False, now=now)[0].action, 'retired')
        fresh = self.fresh()
        retirement = fresh[-1]
        self.assertEqual((retirement['created_ts'], retirement['ts'], retirement['project']),
                         (old, now.isoformat(timespec='seconds'), 'do-it-v2'))
        self.assertTrue(retirement['reason'].startswith('gone:'))
        self.assertEqual(resources.live(fresh), [])
        self.assertEqual(resources.reap(fresh, dry_run=False, now=now), [])
        original = events[-1]
        newer = dict(original, ts=(now + timedelta(hours=1)).isoformat())
        self.assertEqual(resources.live([original, retirement, newer, retirement])[0].created_ts, newer['ts'])
        self.assertEqual(resources.live([original, dict(retirement, subject='someone-else')])[0].ref, ref)
        self.assertEqual(resources.live([original, retirement, retirement]), [])
        resources._append('resource-created', subject, kind='scratch-db', ref=ref, lane='build', ts=old)
        self.assertEqual(resources.live(self.fresh()), [])  # same-process re-record stays the documented open case
        evidence1654('AC4', dict(retirement=retirement, live_after_retirement=0,
                                fresh_interpreter_readback=True, repeat_appends=0))

    def test_1654_retirement_refusals_races_and_caps(self):
        now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        old = (now - timedelta(hours=7)).isoformat()
        path = self.root / 'gone'
        row = dict(created(path, 'scratch-db', 'unknown-run'), ts=old)
        def action(event_row=row, **kw):
            return resources.reap([event_row], dry_run=False, now=now, **kw)[0]
        young = dict(row, ts=(now - timedelta(hours=1)).isoformat())
        self.assertEqual(action(young).reason, 'undetermined: owner')
        self.assertEqual(action(dict(row, lane='')).action, 'refused')
        self.assertEqual(action(dict(row, ts='bad')).action, 'refused')
        real = os.lstat
        def denied(ref, *args, **kw):
            if str(ref) == str(path):
                raise PermissionError('fixture')
            return real(ref, *args, **kw)
        with patch.object(os, 'lstat', side_effect=denied):
            self.assertNotEqual(action().action, 'retired')
        path.mkdir()
        calls = 0
        def reappears(ref, *args, **kw):
            nonlocal calls
            if str(ref) == str(path):
                calls += 1
                if calls == 1:
                    raise FileNotFoundError('first read')
            return real(ref, *args, **kw)
        with patch.object(os, 'lstat', side_effect=reappears):
            self.assertEqual(action().reason, 'undetermined: owner')
        self.assertEqual(action(dict(row, kind='worktree')).reason, 'no-allowed-root')
        path.rmdir()
        self.assertEqual(action(dict(row, kind='worktree')).action, 'retired')
        self.thresholds1654(reap_retire_max_per_pass=2)
        rows = resources.reap([dict(row, ref=str(self.root / f'absent-{i}')) for i in range(5)],
                              dry_run=False, now=now)
        self.assertEqual([r.action for r in rows], ['retired'] * 2 + ['deferred'] * 3)

    def test_1654_census_budget_and_project_reads(self):
        now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        events = finished() + [event('spec-written', 'L-spec-0002')]
        events += [created(self.root / f'gone-{i}') for i in range(3)]
        events += [created(self.directory(f'live-{i}'), subject='L-spec-0002') for i in range(2)]
        events += [created(self.directory(f'dead-{i}')) for i in range(3)]
        self.thresholds1654(reap_max_entries_per_pass=1)
        with (patch.object(disk_reaper, 'process_refs', wraps=disk_reaper.process_refs) as census,
              patch.object(fold, 'fold', wraps=fold.fold) as folded):
            rows = resources.reap(events, dry_run=False, now=now)
        self.assertEqual(census.call_count, 1)
        self.assertLessEqual(folded.call_count, 1)
        self.assertEqual([r.action for r in rows], ['retired'] * 3 + ['refused'] * 2 + ['reaped'] + ['deferred'] * 2)
        evidence1654('AC9', dict(censuses=census.call_count, folds=folded.call_count,
            actions=[r.action for r in rows]))
        for n in (5, 50):
            events = [dict(created(self.root / f'label-{n}-{i}', 'scratch-db', f'run-{n}-{i}'), project='fixture')
                      for i in range(n)]
            for e in events:
                resources._append('resource-created', e['subject'], kind=e['kind'], ref=e['ref'],
                                  lane=e['lane'], project=e['project'], ts=e['ts'])
            snapshot = self.fresh()
            expected = {e['subject']: fold.subject_project(e['subject']) for e in events}
            with (patch.object(fold, 'PROJECT', None),
                  patch.object(fold, 'read_events', wraps=fold.read_events) as reads,
                  patch.object(fold, 'subject_project', wraps=fold.subject_project) as labels):
                rows = resources.reap(events, dry_run=False, now=now)
            self.assertEqual([r.action for r in rows], ['retired'] * n)
            self.assertEqual((reads.call_count, labels.call_count), (0, 0))
            evidence1654('AC11', dict(n=n, retired=len(rows), ledger_reads=reads.call_count,
                subject_project=labels.call_count))
            for e in self.fresh():
                if e['type'] == 'resource-reaped' and e['subject'] in expected:
                    self.assertEqual(e['project'], expected[e['subject']])
        for pinned in (None, 'pinned'):
            path = self.directory('label-removal-' + str(pinned))
            events = [dict(e, project='fixture') for e in finished() + [created(path)]]
            with (patch.object(fold, 'PROJECT', pinned),
                  patch.object(fold, 'subject_project', wraps=fold.subject_project) as labels):
                self.assertEqual(resources.reap(events, dry_run=False)[0].action, 'reaped')
            self.assertEqual(labels.call_count, 0)
            self.assertEqual(self.fresh()[-1]['project'], pinned or 'fixture')
        events = [created(self.root / 'no-project', 'scratch-db', 'unlabelled')]
        report = {}
        token = resources._REAP_REPORT.set(report)
        try:
            with (patch.object(fold, 'PROJECT', None),
                  patch.object(fold, 'subject_project', wraps=fold.subject_project) as labels):
                self.assertEqual(resources.reap(events, dry_run=False, now=now)[0].action, 'retired')
            self.assertEqual(labels.call_count, 1)
            self.assertEqual(report['project_fallback'], 1)
        finally:
            resources._REAP_REPORT.reset(token)

    def test_1654_removal_cap_survives_append_failure(self):
        self.thresholds1654(reap_max_entries_per_pass=1)
        paths = [self.directory(f'append-failure-{i}') for i in range(2)]
        self.assertEqual(sum(p.exists() for p in paths), 2)
        with patch.object(resources, '_append', side_effect=OSError('ledger unavailable')):
            rows = resources.reap(finished() + [created(path) for path in paths], dry_run=False)
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[1].exists())
        self.assertEqual([r.action for r in rows], ['refused', 'deferred'])

    def test_1654_recorded_count_cap_and_hold(self):
        self.thresholds1654(reap_max_entries_per_pass=3)
        paths = [self.directory(f'cap-three-{i}') for i in range(4)]
        events = finished() + [created(path) for path in paths]
        before = self.bytes()
        with (patch.dict(os.environ, DOIT_REAP_HOLD='1'),
              patch.object(disk_reaper, 'process_refs', wraps=disk_reaper.process_refs) as census):
            self.assertEqual([r.action for r in resources.reap(events, dry_run=False)], ['held'] * 4)
            self.assertEqual(census.call_count, 0)
            self.assertEqual([r.action for r in resources.reap(events)], ['would-reap'] * 3 + ['deferred'])
        self.assertEqual(self.bytes(), before)
        self.assertEqual(sum(path.exists() for path in paths), 4)
        self.assertEqual([r.action for r in resources.reap(events, dry_run=False)], ['reaped'] * 3 + ['deferred'])
        self.assertEqual(sum(path.exists() for path in paths), 1)
        evidence1654('AC5', dict(pre_removal_count=4, removed=3, deferred=1,
                                held=4, held_censuses=0, remaining=1))

    def test_reap_safety_paths_and_actor(self):
        path = self.directory("protected")
        link = self.root / "alias"
        link.symlink_to(path, target_is_directory=True)
        self.assert_refused(link, finished() + [created(link)], "undetermined:")
        nested = path / "nested"
        nested.mkdir()
        self.assert_refused(nested, finished() + [created(link / "nested")], "undetermined:")
        self.assert_refused(Path.home(), finished() + [created(Path.home())])
        self.assert_refused(Path("/"), finished() + [created("/")])
        repo, _, _ = self.worktree()
        self.assert_refused(repo, finished() + [created(repo)])
        os.environ["DOIT_LEDGER_FILE"] = "L-builder-local.jsonl"
        self.assert_refused(path, finished() + [created(path)], "actor-not-admitted")

    def test_live_admitted_reaped(self):
        evs = [created("/reused"), event("resource-reaped", ref="/reused")]
        self.assertEqual(resources.live(evs), [])
        evs.append(created("/reused"))
        evs.append(event("resource-reaped", ref="/reused", actor="builder"))
        self.assertEqual([r.ref for r in resources.live(evs)], ["/reused"])
        evs.append(event("resource-reaped", ref="/reused", actor="tick"))
        self.assertEqual(resources.live(evs), [])

    def test_reap_safety_pidfile_and_socket(self):
        path = self.directory("pid-held")
        evs = finished() + [created(path)]
        pidfile = path / "worker.pid"
        pidfile.write_text(str(os.getpid()))
        self.assert_refused(path, evs, "live process")
        pidfile.unlink()
        with (self.proc / "net" / "unix").open("a") as stream:
            stream.write(f"1: 1 0 10000 1 1 123 {path}/socket\n")
        self.assert_refused(path, evs, "live process")

    def test_legacy(self):
        old = self.directory("legacy-worktree-tmpdir")
        os.utime(old, (0, 0))
        self.assertEqual(resources.reap(finished(), dry_run=False), [])
        self.assertTrue(old.exists())
        text = Path(resources.__file__).read_text()
        self.assertFalse(any(term in text for term in ("mtime", "min_age", "st_mtime")))

    def test_emits_and_required(self):
        self.assertEqual(fold_emits.REQUIRED_ROWS["resource-created"], ("kind", "ref", "lane"))
        self.assertEqual(fold_emits.REQUIRED_ROWS["resource-reaped"], ("ref", "reason"))
        command = [str(Path(__file__).resolve().parent.parent / "doit"), "append",
                   "resource-created", "L-spec-0001", "kind=worktree", "lane=build"]
        before = self.bytes()
        denied = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("refused", denied.stdout + denied.stderr)
        self.assertEqual(before, self.bytes())
        accepted = subprocess.run(command + ["ref=/tmp/x"], capture_output=True, text=True)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(len(self.fresh()), 1)
        self.assertNotIn("resource-created", fold_emits.EMITS)
        self.assertEqual(fold_emits.EMITS["resource-reaped"], {"tick", "executor", "operator", "owed-sweeper"})
        self.assertTrue(fold.admits(dict(type="resource-reaped"), "builder"))
        self.assertIsNone(fold.admits(dict(type="resource-reaped"), "tick"))
        for actor in ("builder", "tick", "relay"):
            self.assertIsNone(fold.admits(dict(type="resource-created"), actor))

    def test_reap_round_trip(self):
        path = self.directory("once")
        resources.record("tmpdir", str(path), lane="build", subject="L-spec-0001")
        original = fold.append

        def confirmed_gone(argv, **kwargs):
            self.assertFalse(path.exists())
            return original(argv, **kwargs)

        self.assertEqual(sum(p.exists() for p in [path]), 1)
        with patch.object(fold, "append", side_effect=confirmed_gone):
            rows = resources.reap(finished() + self.fresh(), dry_run=False)
        self.assertEqual(rows[0].action, "reaped")
        reread = self.fresh()
        reaped = [e for e in reread if e["type"] == "resource-reaped"]
        self.assertEqual(len(reaped), 1)
        self.assertEqual(reaped[0]["ref"], str(path))
        self.assertTrue(reaped[0]["reason"])
        before = self.bytes()
        self.assertEqual(resources.reap(finished() + reread, dry_run=False), [])
        self.assertEqual(before, self.bytes())
        print("removal preflight: recorded existing paths=1; fresh-process reaped events=1; repeat=0")

    def test_reap_round_trip_spawn(self):
        path = self.directory("spawn-tmp")
        spawn = "L-grader-0001"
        evs = [event("spawn-done", spawn=spawn), created(path, subject=spawn)]
        self.assertEqual(sum(p.exists() for p in [path]), 1)
        rows = resources.reap(evs, dry_run=False)
        self.assertEqual(rows[0].action, "reaped", rows)
        self.assertEqual(resources.reap(evs + self.fresh(), dry_run=False), [])
        print("removal preflight: spawn-owned paths=1; confirmed gone=1")

    def test_reap_safety_upload_copy_no_allowed_root(self):
        path = self.directory("upload-copy")
        with patch.object(resources, "_path_reason", side_effect=AssertionError("rootless walked")):
            self.assertEqual(resources.reap(finished() + [created(path, "upload-copy")], dry_run=False), [])
        self.assertTrue(path.exists())
        self.assertEqual(self.fresh(), [])

    def builder_record(self, path, kind):
        with patch.dict(os.environ, DOIT_LEDGER_FILE="L-builder-local.jsonl"):
            resources.record(kind, str(path), lane="build", subject="L-spec-0001")
        rows = self.fresh()
        self.assertTrue(all(e["actor"] == "builder" for e in rows))
        return finished() + rows

    def test_path_guard_outside_root(self):
        path = self.directory("outside-worktrees")
        evs = self.builder_record(path, "worktree")
        row = self.assert_refused(path, evs, safety=lambda *_: None)
        self.assertEqual(row.reason, "no-allowed-root")
        self.assertFalse(any(e["type"] == "resource-reaped" for e in self.fresh()))
        self.assertEqual(resources.ROOTS["worktree"](), (fold.ROOT / "worktrees",))

    def test_path_guard_upload_copy(self):
        path = self.directory("upload-copy")
        evs = self.builder_record(path, "upload-copy")
        self.assertEqual(resources.reap(evs, dry_run=False), [])
        self.assertTrue(path.exists())
        self.assertFalse(any(e["type"] == "resource-reaped" for e in self.fresh()))

    def test_path_guard_protected_roots(self):
        home = self.directory("home")
        repo = self.directory("git-repo")
        self.git(repo, "init", "-q")
        with patch.dict(os.environ, HOME=str(home)):
            for path in (Path("/"), home, repo):
                with self.subTest(path=path):
                    # Each ledger is independent so every result targets this path.
                    builder_ledger = self.root / "events" / "L-builder-local.jsonl"
                    if builder_ledger.exists():
                        builder_ledger.unlink()
                    evs = self.builder_record(path, "tmpdir")
                    self.assert_refused(path, evs)
                    before = self.bytes()
                    row = resources.reap(evs)[0]
                    self.assertEqual(row.action, "refused")
                    self.assertTrue(path.exists())
                    self.assertEqual(before, self.bytes())
                    self.assertFalse(any(e["type"] == "resource-reaped" for e in self.fresh()))

    def test_path_guard_symlink_escape_and_root_itself(self):
        allowed = self.directory("allowed")
        outside = self.directory("outside")
        link = allowed / "escape"
        link.symlink_to(outside, target_is_directory=True)
        with patch.dict(resources.ROOTS, tmpdir=lambda: (allowed,)):
            evs = self.builder_record(link, "tmpdir")
            self.assertEqual(self.assert_refused(link, evs).reason, "no-allowed-root")
            self.assertTrue(outside.is_dir())
            self.assertTrue((outside / "payload").is_file())
            self.assertEqual(resources.reap(finished() + [created(allowed)])[0].action, "refused")

    def test_upload_manifest(self):
        config = self.root / "repos/do-it-v2/look.toml"
        config.parent.mkdir(parents=True)
        manifest = self.root / "uploads.jsonl"
        config.write_text('[resources]\nupload_manifest = ' + json.dumps(str(manifest)) + '\n')
        self.assertTrue(resources.live([])[0].undetermined)
        manifest.write_text(json.dumps(dict(kind="upload-copy", ref="/archive/fixture", lane="upload-archive", subject="fixture-sha")) + "\n")
        row = resources.live([])[0]
        self.assertEqual((row.ref, row.source, row.undetermined), ("/archive/fixture", "upload-manifest", None))
        manifest.write_text("malformed\n")
        self.assertTrue(resources.live([])[0].undetermined)

    def test_manifest_split_and_errors(self):
        self.assertEqual(resources.manifest_counts(), {"legacy": 0, "declared": 0})
        config = self.root / "repos/do-it-v2/look.toml"
        config.parent.mkdir(parents=True)
        manifest = self.root / "uploads.jsonl"
        config.write_text('[resources]\nupload_manifest = ' + json.dumps(str(manifest)) + '\n')
        valid = dict(kind="upload-copy", ref=str(self.root / "archive"), lane="archive", subject="fixture")
        missing_kind = {k: v for k, v in valid.items() if k != "kind"} | {"ref": str(self.root / "missing-kind")}
        declared = [valid, valid | {"ref": ""}, missing_kind]
        rows = [{"archived_to": "/legacy/one"}, {}, {"sha": "legacy"}] + declared
        manifest.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        actual = resources._uploads()
        self.assertEqual(len(actual), 3)
        self.assertIsNone(actual[0].undetermined)
        self.assertTrue(all(r.undetermined for r in actual[1:]))
        self.assertEqual([r.undetermined for r in actual[1:]], ["missing or invalid ref", "missing or invalid kind"])
        self.assertTrue(all(r.ref not in {"upload-manifest:0", "upload-manifest:1", "upload-manifest:2"} for r in actual))
        self.assertEqual(resources.manifest_counts(), {"legacy": 3, "declared": 3})
        body = json.dumps(dict(counts=resources.manifest_counts(), resources=[vars(r) for r in actual]), sort_keys=True)
        import hashlib
        print(json.dumps(dict(url="cli://resources/AC2", status=0,
              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
        for bad in (json.dumps(valid) + "\n[]\n" + json.dumps(valid), "malformed", "null"):
            manifest.write_text(bad)
            self.assertEqual(resources.manifest_counts(), {"legacy": 0, "declared": 0, "unreadable": True})
            errors = resources._uploads()
            self.assertEqual(len(errors), 1)
            self.assertIn("upload manifest:", errors[0].undetermined)
        manifest.unlink()
        self.assertTrue(resources.manifest_counts()["unreadable"])
        for kind in (None, "", "tmpdir"):
            manifest.write_text(json.dumps(valid | {"kind": kind}))
            self.assertEqual(resources.manifest_counts(), {"legacy": 0, "declared": 1})
            self.assertIsNotNone(resources._uploads()[0].undetermined)

    def test_reapable_and_exclude(self):
        path = self.directory("excluded")
        evs = finished() + [created(path)]
        with patch.object(resources, "_remove") as remove:
            self.assertEqual(resources.reap(evs, dry_run=False, exclude=frozenset({str(path)})), [])
            remove.assert_not_called()
        self.assertEqual(resources.reap(evs, safety=lambda *_: None)[0].action, "would-reap")
        with patch.dict(resources.ROOTS, tmpdir=lambda: (_ for _ in ()).throw(RuntimeError("root failure"))):
            self.assertTrue(resources.reapable(resources.live(evs)[0]))
            self.assertEqual(resources.not_reapable_counts(evs), {})
            self.assertIn("root failure", resources.reap(evs)[0].reason)
        unknown = created(path, "unknown")
        self.assertTrue(resources.reapable(resources.live([unknown])[0]))
        self.assertIn("invalid kind", resources.reap([unknown])[0].reason)

    def test_creator_release(self):
        path = self.directory("creator")
        row = resources.record("tmpdir", str(path), lane="check", subject="live-operation")
        with self.assertRaises(ValueError):
            resources.release(dict(row), reason="creator-finally")
        with patch.object(resources.shutil, "rmtree", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                resources.release(row, reason="creator-finally")
        self.assertTrue(path.exists())
        self.assertFalse(any(e["type"] == "resource-reaped" for e in self.fresh()))
        resources.release(row, reason="creator-finally")
        self.assertFalse(path.exists())
        self.assertEqual(resources.live(self.fresh()), [])

    def test_actor_admission(self):
        path = self.directory("admission")
        evs = finished() + [created(path)]
        with patch.dict(os.environ, DOIT_LEDGER_FILE="L-build-local.jsonl"):
            row = self.assert_refused(path, evs)
            self.assertEqual(row.reason, "actor-not-admitted")
            self.assertFalse((self.root / "events" / "L-build-local.jsonl").exists())
        original = fold.append

        def strict_append(argv, **kwargs):
            self.assertIs(kwargs.get("strict"), True)
            return original(argv, **kwargs)

        self.assertEqual(sum(p.exists() for p in [path]), 1)
        with patch.object(fold, "append", side_effect=strict_append):
            rows = resources.reap(evs, dry_run=False)
        self.assertEqual(rows[0].action, "reaped")
        self.assertFalse(path.exists())
        self.assertEqual(self.fresh()[0]["actor"], "tick")
        print("removal preflight: admitted paths=1; confirmed gone=1")


if __name__ == "__main__":
    # unittest's -k silently succeeds with zero tests; this script must fail closed.
    names = unittest.defaultTestLoader.getTestCaseNames(ResourcesTests)
    args = sys.argv[1:]
    if args:
        if len(args) != 2 or args[0] != "-k":
            print("usage: test_resources.py [-k substring]", file=sys.stderr)
            sys.exit(2)
        names = [name for name in names if args[1] in name]
        if not names:
            print("no tests match selector: " + args[1], file=sys.stderr)
            sys.exit(2)
    suite = unittest.TestSuite(ResourcesTests(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    body = json.dumps(dict(tests=names, success=result.wasSuccessful(),
                           failures=len(result.failures), errors=len(result.errors)), sort_keys=True)
    print(json.dumps(dict(url='cli://resources/L1654-AC2-AC3-AC4-AC5-AC9-AC11',
        status=0 if result.wasSuccessful() else 1,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
    sys.exit(0 if result.wasSuccessful() else 1)
