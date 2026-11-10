#!/usr/bin/env python3
"""Regression tests for overlap_view (L-spec-2406). Run: python3 test_overlap_view.py [-k PREFIX]

Scratch root only. DOIT_ROOT and HOME are set before any product module is
imported. `gh` is a local stub script reached through DOIT_REAL_GH, so every
call travels the real ghlimit/base_red/overlap_view code; no network, no real gh.
"""
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess as real_sp
import sys
import tempfile
import unittest
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="doit-test-iso-"))
os.environ["HOME"] = str(_TMP / "home")
os.environ["DOIT_ROOT"] = str(_TMP / "root")
sys.path.insert(0, str(HERE))
import fold  # noqa: E402
import ghlimit  # noqa: E402
import overlap_view as ov  # noqa: E402

PROJECT = "albert-scott"
HEAD = "a" * 40
MIB = 1024 * 1024

STUB = '''#!/usr/bin/python3
import json, os, sys
d = os.environ["STUB_DIR"]
a = sys.argv[1:]
with open(os.path.join(d, "log.jsonl"), "a") as f:
    f.write(json.dumps(a) + "\\n")
def out(name, wrap=None):
    p = os.path.join(d, name)
    if not os.path.exists(p):
        sys.stderr.write("HTTP 404\\n"); sys.exit(1)
    data = open(p, "rb").read()
    if wrap:
        data = json.dumps({wrap: json.loads(data)}).encode()
    sys.stdout.buffer.write(data)
if a[:2] == ["api", "rate_limit"]:
    r = {"limit": 5000, "remaining": 5000, "reset": 9999999999}
    print(json.dumps({"resources": {"core": r, "graphql": r}}))
elif len(a) > 1 and "/workflows/coverage-map.yml/runs" in a[1]:
    out("runs.json")
elif len(a) > 1 and "/artifacts?" in a[1]:
    out("artifacts.json", "artifacts")
elif len(a) > 1 and a[1].endswith("/zip"):
    out("zip.bin")
else:
    sys.exit(2)
'''


def mk_map(sha=HEAD, tests=None, unmapped=None, **kw):
    m = {"schema": 1, "sha": sha, "generated_at": "2026-11-10T03:00:00Z",
         "tests": {"tests/test_a.py": ["src/a.py"]} if tests is None else tests,
         "unmapped": [] if unmapped is None else unmapped}
    m.update(kw)
    return m


def mk_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data if isinstance(data, (bytes, str)) else json.dumps(data))
    return buf.getvalue()


def sha_of(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


class Base(unittest.TestCase):
    durations = {"tests/test_a.py": 60.0, "tests/test_b.py": 30.0, "tests/test_c.py": 10.0}

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ov-root-", dir=_TMP))
        self.addCleanup(shutil.rmtree, self.root, True)
        self._old_root = fold.ROOT
        fold.ROOT = self.root
        self.addCleanup(setattr, fold, "ROOT", self._old_root)
        self.stub_dir = self.root / "stub"
        self.stub_dir.mkdir()
        stub = self.stub_dir / "gh"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.env = {"DOIT_ROOT": str(self.root), "HOME": str(self.root / "home"),
                    "DOIT_REAL_GH": str(stub), "STUB_DIR": str(self.stub_dir),
                    "GH_LIMIT_STATE": str(self.root / "gh-state.json")}
        saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        for k, v in saved.items():
            self.addCleanup(self._restore, k, v)
        (self.root / "events").mkdir()
        (self.root / "events" / "L-operator-0001.jsonl").write_text('{"type":"x"}\n')
        self.evid = self.root / "test-evidence" / PROJECT
        self.repo = self.root / "repos" / PROJECT

    @staticmethod
    def _restore(k, v):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    # -- fixtures ---------------------------------------------------------
    def git(self, *a):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        return real_sp.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True,
                           text=True, env=env).stdout.strip()

    def make_checkout(self, dirty_worktree=False):
        self.repo.mkdir(parents=True)
        self.git("init", "-q", "-b", "master")
        self.git("remote", "add", "origin", "https://github.com/acme/albert-scott.git")
        d = self.repo / "scripts" / "ci"
        d.mkdir(parents=True)
        (d / "shard_durations.json").write_text(json.dumps({"files": self.durations}))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "durations")
        self.git("update-ref", "refs/remotes/origin/master", "HEAD")
        self.master_sha = self.git("rev-parse", "origin/master")
        if dirty_worktree:
            # A different working-tree copy, committed nowhere: a worktree read would see it.
            (d / "shard_durations.json").write_text(json.dumps({"files": {"tests/test_zzz.py": 1.0}}))
            self.git("update-index", "--assume-unchanged", "scripts/ci/shard_durations.json")

    def stage_remote(self, *, map_obj=None, zip_bytes=None, artifacts=None, runs=None, head=HEAD):
        s = self.stub_dir
        runs = [{"id": 77, "head_sha": head}] if runs is None else runs
        (s / "runs.json").write_text(json.dumps({"workflow_runs": runs}))
        art = [{"id": 5, "name": "coverage-map", "expired": False, "size_in_bytes": 1234}] \
            if artifacts is None else artifacts
        (s / "artifacts.json").write_text(json.dumps(art))
        if zip_bytes is None:
            zip_bytes = mk_zip({"coverage-map.json": mk_map() if map_obj is None else map_obj})
        (s / "zip.bin").write_bytes(zip_bytes)

    def write_history(self, tests=None):
        self.evid.mkdir(parents=True, exist_ok=True)
        (self.evid / "history.json").write_text(json.dumps(
            {"tests": {"tests/test_a.py": {"runs": 9, "failures": 2, "last_failure": "2026-11-01",
                                           "real": 1, "flaky": 1, "unknown": 0}} if tests is None else tests}))

    def full_setup(self, dirty_worktree=False):
        self.make_checkout(dirty_worktree)
        self.stage_remote()
        self.write_history()

    def calls(self):
        p = self.stub_dir / "log.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def real_calls(self):
        return [c for c in self.calls() if isinstance(c, list) and c[:2] != ["api", "rate_limit"]]

    def listing(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")
                      if "stub" not in p.parts and "gh-state" not in p.name)


# ---------------------------------------------------------------- AC1
class AC1(unittest.TestCase):
    def rows(self, tests, unmapped=()):
        return ov.duplicates(mk_map(tests=tests, unmapped=list(unmapped)))

    def test_ac1_strict_subset_lists_smallest_superset_ties_by_path(self):
        rows = self.rows({"t.py": ["a"], "u1.py": ["a", "b"], "u2.py": ["a", "c"], "u3.py": ["a", "b", "c"]})
        self.assertEqual(rows, [
            {"test": "t.py", "subset_of": "u1.py", "covered": 1},
            {"test": "u1.py", "subset_of": "u3.py", "covered": 2},
            {"test": "u2.py", "subset_of": "u3.py", "covered": 2},
        ])

    def test_ac1_equal_group_lists_all_but_smallest_path(self):
        rows = self.rows({"c.py": ["a"], "a.py": ["a"], "b.py": ["a"], "z.py": ["q"]})
        self.assertEqual(rows, [{"test": "b.py", "subset_of": "a.py", "covered": 1},
                                {"test": "c.py", "subset_of": "a.py", "covered": 1}])

    def test_ac1_equal_pair_both_strict_subsets_of_w(self):
        rows = self.rows({"A.py": ["x", "y"], "B.py": ["y", "x"], "W.py": ["x", "y", "z"]})
        self.assertEqual(rows, [{"test": "A.py", "subset_of": "W.py", "covered": 2},
                                {"test": "B.py", "subset_of": "A.py", "covered": 2}])

    def test_ac1_undetermined_files_never_listed_or_named(self):
        rows = self.rows({"t.py": ["a"], "big_unmapped.py": ["a", "b", "c"], "empty.py": [],
                          "ok.py": ["a", "b"]}, unmapped=["big_unmapped.py", "ghost.py"])
        self.assertEqual(rows, [{"test": "t.py", "subset_of": "ok.py", "covered": 1}])
        named = {r["test"] for r in rows} | {r["subset_of"] for r in rows}
        self.assertTrue(named.isdisjoint({"big_unmapped.py", "empty.py", "ghost.py"}))

    def test_ac1_sorted_exact_keys_and_covered_size(self):
        rows = self.rows({"z.py": ["a"], "m.py": ["a", "b"], "k.py": ["b"], "top.py": ["a", "b", "c"]})
        self.assertEqual([r["test"] for r in rows], sorted(r["test"] for r in rows))
        for r in rows:
            self.assertEqual(set(r), {"test", "subset_of", "covered"})
        self.assertEqual({r["test"]: r["covered"] for r in rows}, {"k.py": 1, "m.py": 2, "z.py": 1})

    def test_ac1_no_duplicates_gives_empty_list(self):
        self.assertEqual(self.rows({"a.py": ["x"], "b.py": ["y"]}), [])


# ---------------------------------------------------------------- AC2
class AC2(Base):
    def test_ac2_prefix_is_minimal_and_cut_at_eighty_percent(self):
        d = {"a": 50.0, "b": 30.0, "c": 10.0, "d": 5.0, "e": 5.0}
        self.assertEqual([r["test"] for r in ov.slowest(d, {"tests": {}})], ["a", "b"])  # exactly 80
        d = {"a": 40.0, "b": 30.0, "c": 20.0, "d": 10.0}
        self.assertEqual([r["test"] for r in ov.slowest(d, {"tests": {}})], ["a", "b", "c"])  # 70 < 80
        d = {"a": 79.0, "b": 21.0}
        self.assertEqual([r["test"] for r in ov.slowest(d, {"tests": {}})], ["a", "b"])  # 79 < 80

    def test_ac2_ties_by_path_ascending_and_empty_total(self):
        d = {"z": 10.0, "b": 10.0, "m": 10.0, "a": 10.0, "q": 10.0}
        self.assertEqual([r["test"] for r in ov.slowest(d, {"tests": {}})], ["a", "b", "m", "q"])
        self.assertEqual(ov.slowest({}, {"tests": {}}), [])
        self.assertEqual(ov.slowest({"a": 0.0}, {"tests": {}}), [])

    def test_ac2_history_row_or_null_exact_keys(self):
        row = {"runs": 3, "failures": 1, "last_failure": "x", "real": 1, "flaky": 0, "unknown": 0}
        rows = ov.slowest({"a": 60.0, "b": 40.0}, {"tests": {"a": row}})
        self.assertEqual(rows[0], {"test": "a", "seconds": 60.0, "history": row})
        self.assertIsNone(rows[1]["history"])
        self.assertTrue(json.dumps(rows[1]).count('"history": null') == 1)
        for r in rows:
            self.assertEqual(set(r), {"test", "seconds", "history"})

    def test_ac2_bad_durations_raise(self):
        with self.assertRaises(ValueError):
            ov.slowest({"a": "x"}, {"tests": {}})
        with self.assertRaises(ValueError):
            ov.slowest({"a": -1.0}, {"tests": {}})

    def test_ac2_durations_read_from_origin_master_not_working_tree(self):
        self.make_checkout(dirty_worktree=True)
        files, sha = ov.read_durations(PROJECT)
        self.assertEqual(files, self.durations)
        self.assertEqual(sha, self.master_sha)
        self.assertNotIn("tests/test_zzz.py", files)

    def test_ac2_build_records_durations_sha(self):
        self.full_setup(dirty_worktree=True)
        paths = ov.build(PROJECT)
        src = json.loads((self.evid / "overlap-source.json").read_text())
        self.assertEqual(src, {"map_sha": HEAD, "durations_sha": self.master_sha})
        slow = json.loads(paths["slowest"].read_text())
        self.assertEqual([r["test"] for r in slow], ["tests/test_a.py", "tests/test_b.py"])
        self.assertEqual(slow[0]["history"]["failures"], 2)
        self.assertIsNone(slow[1]["history"])

    def test_ac2_missing_checkout_ref_raises(self):
        self.make_checkout()
        self.git("update-ref", "-d", "refs/remotes/origin/master")
        with self.assertRaises(ValueError):
            ov.read_durations(PROJECT)


# ---------------------------------------------------------------- AC3
class AC3(Base):
    def setUp(self):
        super().setUp()
        self.make_checkout()
        self.gate_ok = True
        real_gate = ghlimit.gate

        def gate(wait=True):
            with open(self.stub_dir / "log.jsonl", "a") as f:
                f.write(json.dumps({"gate": {"wait": wait}}) + "\n")
            return real_gate(wait=wait) if self.gate_ok else {"ok": False, "remaining": 0, "reset": 0, "waited_s": 0}
        orig = ghlimit.gate
        ghlimit.gate = gate
        self.addCleanup(setattr, ghlimit, "gate", orig)

    def downloads(self):
        return [c for c in self.calls() if isinstance(c, list) and len(c) > 1 and c[1].endswith("/zip")]

    def refuses(self, **stage):
        self.stage_remote(**stage)
        with self.assertRaises(ValueError):
            ov.fetch_map(PROJECT)
        d = self.evid / "coverage-map"
        self.assertEqual(list(d.glob("*")) if d.exists() else [], [])

    def test_ac3_success_path_and_cache(self):
        self.stage_remote()
        p = ov.fetch_map(PROJECT)
        self.assertEqual(p, self.evid / "coverage-map" / f"{HEAD}.json")
        self.assertEqual(json.loads(p.read_text())["sha"], HEAD)
        self.assertEqual(list(p.parent.iterdir()), [p])
        runs = [c for c in self.real_calls() if len(c) > 1 and "/workflows/coverage-map.yml/runs" in c[1]]
        self.assertEqual(len(runs), 1)
        self.assertIn("branch=master", runs[0][1])
        self.assertIn("status=success", runs[0][1])
        self.assertIn("per_page=1", runs[0][1])
        self.assertNotIn("--paginate", runs[0])
        order = [("gate" if "gate" in c else "zip" if c[1].endswith("/zip") else
                  "art" if "/artifacts?" in c[1] else "run" if "/workflows/" in c[1] else "rl")
                 for c in self.calls() if isinstance(c, dict) or len(c) > 1]
        self.assertLess(len(order) - 1 - order[::-1].index("gate"), order.index("zip"))
        self.assertLess(order.index("art"), order.index("zip"))
        self.assertIn({"gate": {"wait": False}}, self.calls())
        self.assertEqual(len(self.downloads()), 1)
        ov.fetch_map(PROJECT)
        self.assertEqual(len(self.downloads()), 1)

    def test_ac3_no_run(self):
        self.refuses(runs=[])

    def test_ac3_no_artifact(self):
        self.refuses(artifacts=[])

    def test_ac3_only_expired_artifact(self):
        self.refuses(artifacts=[{"id": 5, "name": "coverage-map", "expired": True, "size_in_bytes": 10}])

    def test_ac3_two_artifacts(self):
        a = {"id": 5, "name": "coverage-map", "expired": False, "size_in_bytes": 10}
        self.refuses(artifacts=[a, dict(a, id=6)])

    def test_ac3_gate_low_no_download(self):
        self.gate_ok = False
        self.refuses()
        self.assertEqual(self.downloads(), [])

    def test_ac3_size_missing_or_over_cap_no_download(self):
        self.refuses(artifacts=[{"id": 5, "name": "coverage-map", "expired": False}])
        self.refuses(artifacts=[{"id": 5, "name": "coverage-map", "expired": False, "size_in_bytes": "9"}])
        self.refuses(artifacts=[{"id": 5, "name": "coverage-map", "expired": False,
                                 "size_in_bytes": 64 * MIB + 1}])
        self.assertEqual(self.downloads(), [])

    def test_ac3_download_over_cap(self):
        self.refuses(zip_bytes=b"\0" * (64 * MIB + 1))
        self.assertEqual(len(self.downloads()), 1)

    def test_ac3_bad_zip_members(self):
        self.refuses(zip_bytes=mk_zip({"coverage-map.json": mk_map(), "extra.json": "{}"}))
        self.refuses(zip_bytes=mk_zip({"other.json": mk_map()}))
        self.refuses(zip_bytes=b"not a zip")

    def test_ac3_sha_differs_from_head(self):
        self.refuses(map_obj=mk_map(sha="b" * 40))

    def test_ac3_map_fails_sd3_validation(self):
        self.refuses(map_obj=mk_map(schema=2))
        self.refuses(map_obj=mk_map(sha="abc"))
        self.refuses(map_obj=mk_map(tests={"t.py": "src/a.py"}))
        self.refuses(map_obj=mk_map(tests={"t.py": [1]}))
        self.refuses(map_obj=mk_map(unmapped="t.py"))
        self.refuses(map_obj=mk_map(generated_at="yesterday"))

    def test_ac3_download_command_has_explicit_timeout(self):
        self.stage_remote()
        seen = {}
        real_run = real_sp.run

        def spy(cmd, *a, **k):
            if cmd[1:2] == ["api"] and str(cmd[2]).endswith("/zip"):
                seen.update(k)
            return real_run(cmd, *a, **k)
        ov.subprocess.run = spy
        self.addCleanup(setattr, ov.subprocess, "run", real_run)
        ov.fetch_map(PROJECT)
        self.assertEqual(seen.get("timeout"), 120)


# ---------------------------------------------------------------- AC4
class AC4(Base):
    def cli(self):
        return real_sp.run(["/usr/bin/python3", "-B", str(HERE / "overlap_view.py"), PROJECT],
                           capture_output=True, text=True, env=dict(os.environ, **self.env))

    def test_ac4_round_trip_across_processes(self):
        self.full_setup()
        paths = ov.build(PROJECT)
        self.assertEqual(paths, {"duplicates": self.evid / "duplicates.json", "slowest": self.evid / "slowest.json"})
        first = {k: json.loads(p.read_text()) for k, p in paths.items()}
        proc = self.cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), [str(paths["duplicates"]), str(paths["slowest"])])
        for k, p in paths.items():
            self.assertEqual(json.loads(p.read_text()), first[k])
        self.assertEqual([n for n in os.listdir(self.evid) if ".tmp" in n], [])
        self.assertEqual([n for n in os.listdir(self.evid / "coverage-map") if ".tmp" in n], [])

    def test_ac4_failure_between_renames_keeps_previous_pair(self):
        self.full_setup()
        paths = ov.build(PROJECT)
        before = {n: (self.evid / n).read_bytes() for n in
                  ("duplicates.json", "slowest.json", "overlap-source.json")}
        self.write_history({"tests/test_b.py": {"runs": 1, "failures": 0, "last_failure": None,
                                                "real": 0, "flaky": 0, "unknown": 0}})
        for fail_on in (1, 2, 3):
            n = [0]

            def flaky(src, dst, fail_on=fail_on):
                n[0] += 1
                if n[0] == fail_on:
                    raise OSError("simulated")
                os.replace(src, dst)
            ov._replace = flaky
            try:
                with self.assertRaises(ValueError):
                    ov.build(PROJECT)
            finally:
                ov._replace = os.replace
            for name, data in before.items():
                self.assertEqual((self.evid / name).read_bytes(), data, f"fail_on={fail_on} {name}")
            self.assertEqual([x for x in os.listdir(self.evid) if ".tmp" in x], [])
        ov.build(PROJECT)  # and with no failure the new history lands
        self.assertNotEqual((self.evid / "slowest.json").read_bytes(), before["slowest.json"])

    def test_ac4_first_build_failure_leaves_nothing(self):
        self.full_setup()
        n = [0]

        def flaky(src, dst):
            n[0] += 1
            if n[0] == 2:
                raise OSError("simulated")
            os.replace(src, dst)
        ov._replace = flaky
        try:
            with self.assertRaises(ValueError):
                ov.build(PROJECT)
        finally:
            ov._replace = os.replace
        self.assertFalse((self.evid / "duplicates.json").exists())
        self.assertFalse((self.evid / "slowest.json").exists())
        self.assertEqual([x for x in os.listdir(self.evid) if ".tmp" in x], [])


# ---------------------------------------------------------------- AC5
class AC5(Base):
    OUT = ("duplicates.json", "slowest.json", "overlap-source.json")

    def snapshot(self):
        return {n: (self.evid / n).read_bytes() for n in self.OUT if (self.evid / n).exists()}

    def ledger_hash(self):
        return {p.name: sha_of(p) for p in (self.root / "events").glob("*.jsonl")}

    def test_ac5_each_failure_raises_and_leaves_previous_pair(self):
        self.full_setup()
        ov.build(PROJECT)
        before = self.snapshot()
        self.assertEqual(set(before), set(self.OUT))

        def run_failing(label, breakit, restore):
            breakit()
            try:
                with self.assertRaises(ValueError, msg=label):
                    ov.build(PROJECT)
                self.assertEqual(self.snapshot(), before, label)
            finally:
                restore()

        shutil.move(self.evid / "coverage-map", self.root / "cm-aside")
        (self.stub_dir / "runs.json").write_text(json.dumps({"workflow_runs": []}))
        run_failing("map fetch", lambda: None, lambda: None)
        (self.stub_dir / "runs.json").write_text(json.dumps({"workflow_runs": [{"id": 77, "head_sha": HEAD}]}))
        shutil.move(self.root / "cm-aside", self.evid / "coverage-map")

        hist = (self.evid / "history.json").read_bytes()
        run_failing("history missing", lambda: (self.evid / "history.json").unlink(),
                    lambda: (self.evid / "history.json").write_bytes(hist))
        run_failing("history not JSON", lambda: (self.evid / "history.json").write_text("{nope"),
                    lambda: (self.evid / "history.json").write_bytes(hist))
        run_failing("history no tests", lambda: (self.evid / "history.json").write_text("[]"),
                    lambda: (self.evid / "history.json").write_bytes(hist))
        run_failing("durations unreadable", lambda: self.git("update-ref", "-d", "refs/remotes/origin/master"),
                    lambda: self.git("update-ref", "refs/remotes/origin/master", self.master_sha))
        ov.build(PROJECT)  # all restored: builds again

    def test_ac5_failure_on_empty_store_writes_nothing(self):
        self.make_checkout()
        self.stage_remote()
        with self.assertRaises(ValueError):  # history.json missing
            ov.build(PROJECT)
        for n in self.OUT:
            self.assertFalse((self.evid / n).exists())

    def test_ac5_cli_failure_exits_one_with_reason_and_no_stdout(self):
        self.full_setup()
        (self.evid / "history.json").unlink()
        proc = real_sp.run(["/usr/bin/python3", "-B", str(HERE / "overlap_view.py"), PROJECT],
                           capture_output=True, text=True, env=dict(os.environ, **self.env))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("history.json", proc.stderr)
        proc = real_sp.run(["/usr/bin/python3", "-B", str(HERE / "overlap_view.py"), "no-such-project"],
                           capture_output=True, text=True, env=dict(os.environ, **self.env))
        self.assertEqual((proc.returncode, proc.stdout), (1, ""))

    def test_ac5_reader_only_ledger_and_checkout_untouched(self):
        self.full_setup()
        ledger = self.ledger_hash()
        before = set(self.listing())
        ov.build(PROJECT)
        ov.build(PROJECT)
        self.assertEqual(self.ledger_hash(), ledger)
        created = set(self.listing()) - before
        self.assertTrue(created)
        for c in created:
            self.assertTrue(c == "test-evidence" or c.startswith(f"test-evidence/{PROJECT}"), c)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "origin/master"), self.master_sha)


if __name__ == "__main__":
    unittest.main()
