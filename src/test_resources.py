#!/usr/bin/env python3
"""Resource lifecycle checks against private ledgers and real filesystem/git state."""
import atexit
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Do this before importing fold, including under the unchanged ./doit test runner.
_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix="doit-resources-")
atexit.register(_IMPORT_ROOT.cleanup)
os.environ["DOIT_ROOT"] = _IMPORT_ROOT.name
if Path(os.environ["DOIT_ROOT"]).resolve() == (Path.home() / ".do-it").resolve():
    raise RuntimeError("resource tests require a throwaway DOIT_ROOT")
os.environ.pop("DOIT_PROJECT", None)
os.environ["DOIT_LEDGER_FILE"] = "L-tick-local.jsonl"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fold
import resources
import fold_emits


def event(event_type, subject="L-spec-0001", **fields):
    return dict(type=event_type, subject=subject, actor=fields.pop("actor", "operator"),
                ts="2026-10-23T00:00:00+00:00", **fields)


def created(path, kind="tmpdir", subject="L-spec-0001"):
    return event("resource-created", subject, kind=kind, ref=str(path), lane="build")


def finished(subject="L-spec-0001"):
    return [event("spec-written", subject), event("spec-killed", subject)]


class ResourcesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="doit-resource-case-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
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
        with patch.object(resources.reap_tmp, "_refs_c", side_effect=PermissionError("unreadable pid")):
            self.assert_refused(path, evs, "undetermined:")

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
    sys.exit(0 if result.wasSuccessful() else 1)
